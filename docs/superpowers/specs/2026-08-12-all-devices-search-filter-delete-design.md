# All Devices: search/filter, and delete for revoked devices

**Date:** 2026-08-12
**Status:** implemented
**Touches:** `apps/api_gateway` static admin UI + gateway route/store. No firmware change.

## Problem

The admin-only "All Devices" table (`apps/api_gateway/app/static/js/devices.js`,
rendered into `#device-all-list`) lists every paired device across every user with no
way to narrow the list, and no way to remove a device once it's been revoked — revoked
rows just sit there dimmed forever. There's no pagination and no ceiling on row count
today; the table is expected to stay in the dozens, not thousands, but even at that
scale an admin hunting for one device or cleaning up old hardware has to scroll and
read every row by eye.

## Decision

Two independent, additive changes to the All Devices table only. My Devices,
pairing, and profile reassignment are untouched.

1. A filter bar (profile, status, free-text search) above the table, filtering
   client-side over data already fetched — no new API calls.
2. A per-row **Delete** button, enabled only once a device is revoked, that
   permanently removes the device row via a new admin-only endpoint.

## 1. Search & filter

### UI (`index.html`)

A `.row.tight` filter bar inserted between the "Every paired device…" hint and
`#device-all-list`, styled identically to the Model Registry filter bar
(`index.html:738-761`):

```html
<div class="row tight">
  <label>
    Profile
    <select id="device-all-filter-profile">
      <option value="">All</option>
      <option value="__unassigned__">Unassigned</option>
      <!-- populated with profile names -->
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
```

### Logic (`devices.js`)

- `renderAllDeviceFilterProfileOptions()`, modeled on the existing
  `renderDevicePairProfileSelect()`: populates `#device-all-filter-profile` from
  `profileData` keys (sorted), preserving the current selection across re-population.
  Called once at the end of `maybeLoadAllDevices()`, after `allDeviceData` loads.
- `_filteredAllDeviceData()`, modeled on `model-registry.js`'s
  `_filteredRegistryData()`:

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

- `renderAllDeviceList()` renders `_filteredAllDeviceData()` instead of
  `allDeviceData` directly. Empty-state message: `"No devices match the current
  filters."` when `allDeviceData.length` is nonzero but the filtered result is empty,
  otherwise the existing `"No devices paired yet."`.
- Listeners at the bottom of the file, matching the existing guarded style:
  `change` on both selects, `input` on the search box, all calling
  `renderAllDeviceList()`. Pure re-render — no refetch, so filters have no effect on
  My Devices or on network traffic.
- Filter state lives only in the DOM controls, so it naturally survives a
  `renderAllDeviceList()` triggered by revoke/delete actions (those call
  `maybeLoadAllDevices()`, which refetches `allDeviceData` and re-renders, but never
  touches the filter inputs). Not persisted across a page reload.

No backend change: `name`, `serial`, `owner_username`, `profile_id`, and `revoked`
are already present in every `/v1/devices` row (`services/auth/devices.py:_device_dict`
+ `owner_username` added in the route handler).

## 2. Delete a revoked device

Hard delete, admin-only, and only for a device that has already been revoked. A
device that isn't revoked can't be deleted — it must be revoked first, same click
sequence an admin already uses today, just one more step for a safety gate.

### Store (`services/auth/devices.py`)

```python
async def delete(self, device_id: str) -> bool:
    """Hard-delete a device row. Caller is responsible for checking it's revoked
    first -- this method does not enforce that, it just removes the row."""
    async with db_session() as s:
        row = await s.get(Device, device_id)
        if row is None:
            return False
        await s.delete(row)
        await s.commit()
        return True
```

Shaped like `provider_store.delete()` (`services/providers/store.py:93-102`), the
existing hard-delete precedent in this codebase.

### Route (`api/routes/devices.py`)

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

`DELETE /v1/devices/{device_id}` falls under the existing `_ADMIN_PREFIXES` entry for
`"/v1/devices"` (`core/auth_guard.py:98`) — this is a segment-aware prefix match, so
the new method+path is already admin-gated with no `auth_guard.py` edit. Confirmed
against `test_every_mounted_path_is_classified` in
`tests/unit/http/test_auth_guard_route_coverage.py`, which would fail if any mounted
path classified to `None`; `/v1/devices/{id}` already classifies to `"admin"` today
for the existing revoke route on the same prefix.

### UI (`devices.js`)

In `renderAllDeviceList()`'s actions column, add a second button next to the existing
Revoke button:

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
}
```

Handler, next to `revokeAnyDevice`:

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

Wired the same way as the existing revoke button, in `renderAllDeviceList()`:

```js
table.querySelectorAll("[data-device-delete]").forEach((btn) =>
  btn.addEventListener("click", () => deleteAnyDevice(btn.getAttribute("data-device-delete")))
);
```

## Explicitly out of scope

- **My Devices** gets no delete capability — self-service deletion of a user's own
  revoked devices is a separate feature, not requested here.
- **No bulk delete.** The existing bulk-revoke toolbar (`renderDataTable`'s
  `bulkActions`) is untouched; delete stays a per-row action only.
- **No pagination.** Out of scope for this change; noted as a pre-existing gap the
  filter bar happens to make less painful, not something this design fixes.
- **Filter state is not persisted** across page reloads or shared via URL — this
  matches the Model Registry filter bar's behavior today.

## Testing

**Gateway** (`tests/unit/auth/test_devices_routes.py`)
- `DELETE /v1/devices/{id}` on a device that doesn't exist → 404.
- `DELETE /v1/devices/{id}` on a device that exists but isn't revoked → 400.
- Revoke, then `DELETE` → 200, and the device is absent from a subsequent
  `GET /v1/devices`.
- (Existing `_logged_in_user` fixture already resolves to an admin actor in this
  test file's setup, per `test_admin_lists_and_revokes_any_device`.)

**Static admin UI** — no automated test harness exists for this vanilla-JS panel
(confirmed: no test file references `devices.js`). Verified manually: filter bar
narrows the table correctly for each control and in combination; Delete stays
disabled until a row is revoked; clicking Delete on a revoked row removes it after
confirmation; clicking Delete's underlying endpoint on a non-revoked device (e.g. via
a stale disabled-button bypass) surfaces the 400 message via `device-status`.
